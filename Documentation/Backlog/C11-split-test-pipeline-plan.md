# C11 — Split test_pipeline.py into Subfolder
**Purpose**: Reduce full test suite wall time from ~7–8 min to ≤150s by giving each of three pipeline test files its own xdist worker instead of one monolithic file pinning the critical path.
**Audience**: Developers iterating locally and local CI pipelines waiting on test feedback.
**Status**: To Do

---

## Background
`tests/test_pipeline.py` (223 tests, ~355s serial runtime) is the bottleneck for the entire test suite under `--dist=loadfile` — one xdist worker holds it alone. Splitting it into three files under `tests/pipeline/` lets at least three workers run in parallel, cutting the wall-time critical path to the longest single-file serial time (~150s target for the ingest file). This is a pure file reorganisation: no test logic, assertions, mocks, or fixture parameters change.

**Note**: CI workflows run with `-n0` (serial), so this split does not reduce CI wall time. The improvement is for local developer iteration only.

## Goal
After this work, `uv run pytest --no-cov` on a ≥4-core machine completes in ≤150s; `uv run pytest` (with coverage) passes the 85% gate; `uv run pytest tests/pipeline/ --collect-only -q` shows BASELINE_COUNT collected tests (measured before deletion) (same as the original file before deletion); all tests pass in serial mode (`-n0`); no cross-file duplicate test function names exist.

---

## Scope

### In Scope
- Creating `tests/pipeline/` with `__init__.py`, `conftest.py`, and three test files
- Moving all BASELINE_COUNT tests (estimated ~223) with zero logic changes — assertions, mocks, fixture parameters unchanged
- Deleting `tests/test_pipeline.py`
- Verifying wall-time improvement and coverage gate after the split
- Updating `Documentation/Architecture/200_testing_strategy.md` to reflect the new `tests/pipeline/` location

### Out of Scope
- Changing any test logic, assertions, or mock behaviour
- Splitting other slow files (`test_sync_e2e.py`, `test_pipeline_ingest_directory_fts.py`)
- `asyncio_default_fixture_loop_scope = "module"` optimisation (deferred)
- Any production code changes

---

## Acceptance criteria

> Acceptance criteria are verified in the final task. See [Task F.1 — Final verification & documentation update].

---

## What does NOT change
- All test assertions, mock setups, and fixture parameters in `test_pipeline.py`
- All other test files in `tests/` root (e.g. `test_pipeline_acl.py`, `test_pipeline_metadata.py`)
- `tests/conftest.py` — root conftest remains untouched
- Coverage threshold (`--cov-fail-under=85`)
- CI workflow flags (`-n0` already set; unaffected by this split)

---

## Known limitations / accepted trade-offs
- Up to 3 concurrent `connected_store` instances after split (one per worker per split file) vs 1 today; the edge-case tests at lines 6490–6508 may also bring `connected_store` into the multi file, making all three files use it. Each spawns a Tokio thread pool. Accepted given workers run in parallel.
- Wall-time target assumes ≥4 cores (`-n auto` ≥ 3 workers). Improvement is proportionally less on fewer cores.
- If `test_pipeline_ingest.py` still exceeds 150s serial after the split, the contingency is a further ingest split into `test_pipeline_ingest_file.py` and `test_pipeline_ingest_directory.py` (out of scope for C11).

---

## Architecture

### New directory structure
```
tests/
  pipeline/
    __init__.py          # empty — consistent with tests/cli/, tests/server/, etc.
    conftest.py          # shared helpers (non-fixture plain functions)
    test_pipeline_ingest.py   # ~83 ingest tests + section-local helpers
    test_pipeline_search.py   # ~57 search/context/trace tests + section-local helpers
    test_pipeline_multi.py    # ~83 multi-collection/recompute/RAG fusion tests + section-local helpers
```

### conftest.py contents
Plain Python functions and classes — **no `@pytest.fixture` decorators**. Tests call them directly.

**Pytest does NOT auto-inject plain functions from conftest.py** (only `@pytest.fixture`-decorated callables). Each test file must import these explicitly using a relative import: `from .conftest import MockEmbedderBackend, ...`. Do NOT use bare `from conftest import ...` — the root `tests/conftest.py` inserts `tests/` into sys.path first, so that form would resolve to the root conftest.

Shared helpers moved from `tests/test_pipeline.py`:
| Symbol | Source line | Consumers |
|---|---|---|
| `class MockEmbedderBackend` | 22 | all three files |
| `class MockRerankerBackend` | 32 | all three files |
| `def make_embedder()` | 41 | all three files |
| `def make_reranker()` | 45 | all three files |
| `def make_pipeline(store)` | 53 | all three files |

Group-C helpers (`_scored`, `_meta`, `_search_many_pipeline` at lines 3055–3131) may go in conftest or stay local to `test_pipeline_multi.py` — implementer decides at split time based on whether they are used by any search file tests.

### Section-local helper routing
Run `grep -n '^def [^t]\|^class [A-Z]' tests/test_pipeline.py` before splitting to enumerate the full list. Apply these pre-assignments:

| Helper | Likely target file |
|---|---|
| `_make_scored_candidate` (line 1776) | search (used by eval trace tests) |
| `_meta` (~3077) | multi (`_scored`, `_meta`, `_search_many_pipeline` are Group-C helpers — already mentioned in prose but missing from table) |
| `_make_stub_store_for_embedding_tests` (line 3517) | ingest (its consumers call pipeline.ingest_directory() — lines 3558, 3582, 3611 — so Rule 1 applies) |
| `_make_pipeline_for_embedding_tests` (~3534) | ingest (co-located with and consumed by the same ingest tests as `_make_stub_store_for_embedding_tests`) |
| `_make_pipeline_for_recompute` (line 3961) | ingest (recompute = ingest group) |
| `_make_mock_store_for_b5` (~3790) | ingest (consumed by recompute tests at lines ~3831–3913 and ingest tests at ~4298–4337) |
| `_make_pipeline_with_store` (~3809) | ingest (consumed by ingest recompute trigger tests at ~3834–3913) |
| `_make_embedder_with_model` (line 4373) | ingest |
| `_make_mock_store_c1` (line 4388) | ingest |
| `_make_mock_store_for_c2` (line 4845) | ingest (multilingual ingest, mock-based) |
| `_make_pipeline_with_detector` (line 4869) | ingest |
| `_make_candidate` (line 5641) | multi (used by RAG fusion tests) |
| `_make_rag_fusion_search_many_pipeline` (line 6583) | multi |
| `_rag_scored` (line 6635) | multi |
| `_make_search_result` (line 2550) | search (used by filter/ACL tests) |

### Test assignment rules (apply in order)
1. Calls `pipeline.ingest_file()`, `pipeline.ingest_directory()`, or `pipeline.recompute_collection_meta()` → **ingest**
2. Calls `pipeline.search()`, `pipeline.search_with_context()`, `pipeline.explain()`, or tests single-collection result shapes → **search**
3. Calls `pipeline.search_many()`, `_fuse_rag_fusion_results`, or tests multi-collection fanout → **multi**
4. Pure unit tests of private helpers → go with the public method they serve
5. Multi-category tests (ingest-then-search round-trip) → assign to the file of the **last pipeline method called** in the test body. For example, a test that calls ingest_file() then search() goes to **search**.

Edge case to verify: `test_store_has_vector_index_true_for_normal_collection` (line 6490), `test_store_has_vector_index_false_for_missing_collection` (line 6501), `test_hybrid_search_with_trace_filters_applied` (line 6508) use `connected_store`. If they land in the multi file, confirm they are truly search_many context — otherwise move to search or ingest.

> **Edge case — `explain()` with RAG fusion**: `test_explain_rag_fusion_*` tests call `pipeline.explain()` with RAG fusion config. By Rule 2 alone this would go to search; however, these tests exercise multi-collection fanout semantics. Assign them to the **multi** file. If their helpers (`_make_rag_fusion_search_many_pipeline`) are already in multi, this assignment is consistent.

---

## Task breakdown

### Phase 1 — Scaffold
> **Releasable**: after Task 1.1, the new directory structure exists and pytest can discover it. No tests run from it yet.

#### Task 1.1 — Create `tests/pipeline/` directory scaffolding
- [x] **File**: `tests/pipeline/__init__.py`, `tests/pipeline/conftest.py`
- **Depends on**: nothing
- **Description**:
  - Run `grep -n '^def [^t]\|^class [A-Z]' tests/test_pipeline.py` and record the output as a reference for the split (do not commit this; it is a planning step only).
  - Run `uv run pytest tests/test_pipeline.py --collect-only -q --no-cov | tail -1` and record the **actual** collected test count as `BASELINE_COUNT`. Use this number — not the ~223 estimate — for all subsequent sum checks.
  - Create `tests/pipeline/__init__.py` as an empty file (no content, no docstring) — consistent with `tests/cli/__init__.py`, `tests/server/__init__.py`, etc.
  - Create `tests/pipeline/conftest.py` containing exactly:
    - All necessary imports from `tests/test_pipeline.py` required by the five shared helpers below
    - `class MockEmbedderBackend` (lines 22–31 of `test_pipeline.py`)
    - `class MockRerankerBackend` (lines 32–40)
    - `def make_embedder() -> Embedder` (lines 41–44)
    - `def make_reranker() -> Reranker` (lines 45–52)
    - `def make_pipeline(store)` (lines 53–74)
  - **Do NOT** add `@pytest.fixture` decorators — these are plain Python callables.
  - **Do NOT** move any `test_` functions into conftest.
  - The `_scored`, `_meta`, `_search_many_pipeline` group-C helpers may optionally be included here; decide based on whether any of the search-file tests call them (check with grep before deciding).
- **Releasable**: after this task, `tests/pipeline/__init__.py` and `tests/pipeline/conftest.py` exist and import cleanly. No test functions exist yet.
- **Tests (TDD)** — `tests/pipeline/`:
  - Smoke: `uv run python -c "import tests.pipeline.conftest"` — must not raise ImportError
  - Collect: `uv run pytest tests/pipeline/ --collect-only -q --no-cov` — must show `0 tests collected` (no test functions yet)
  - Checkpoint: `uv run pytest tests/pipeline/ --collect-only -q --no-cov`

---

### Phase 2 — Populate split files
> **Releasable**: after each task, the corresponding file's tests can be run independently. After Task 2.3, all BASELINE_COUNT tests live in `tests/pipeline/` (while `test_pipeline.py` still exists — do not delete yet until Phase 3 confirms zero regressions).

> **⚠️ Full-suite runs (`uv run pytest`) are broken during Phase 2**: `tests/test_pipeline.py` still exists while split files are being added. Running the full suite will double-collect and potentially double-run tests. During Phase 2, only run targeted single-file checkpoints (`uv run pytest tests/pipeline/test_pipeline_ingest.py -n0 ...`), never the full suite. The full suite is only safe to run after Task 3.1 deletes the original file.

> **Note on locally-defined fixtures**: `tests/test_pipeline.py` may contain `@pytest.fixture`-decorated functions that are defined locally (not in `tests/conftest.py`) and used only by tests in that file. These are distinct from the plain helper functions in `conftest.py`. When moving tests, carry any `@pytest.fixture` functions they depend on into the same split file. Run `grep -n '^@pytest.fixture' tests/test_pipeline.py` before splitting to inventory these.

#### Task 2.1 — Create `tests/pipeline/test_pipeline_ingest.py`
- [x] **File**: `tests/pipeline/test_pipeline_ingest.py`
- **Depends on**: Task 1.1
- **Description**:
  - Apply the assignment rules to identify all ingest tests. Ingest group covers: `ingest_file`, `ingest_directory`, format handling, chunking, centroid computation, language detection during ingest, PDF/image/markdown format-specific ingest paths, recompute_collection_meta, and tests of the pipeline factory wiring.
  - Move into this file all `test_` functions whose primary call is to `pipeline.ingest_file()`, `pipeline.ingest_directory()`, or `pipeline.recompute_collection_meta()`. Also move pure structural tests like `test_create_pipeline_wires_all_components`, `test_pipeline_stores_fanout_params`, `test_pipeline_default_fanout_params_match_config`, `test_create_pipeline_*`, `test_ragpipeline_*`, `test_self_embedder_does_not_exist`, `test_no_self_embedder_in_pipeline`, `test_no_embedding_model_attribute_accesses`, `test_no_underscore_embedder_anywhere`, `test_ingest_result_*`, `test_ingest_file_records_parse_embed_persist` (ingest-path recorder), `test_pipeline_noop_when_unbound` (last call is `ingest_file` → Rule 5 routes to ingest).
  - Move section-local helpers used exclusively by these tests: `_make_pipeline_for_recompute`, `_make_embedder_with_model`, `_make_mock_store_c1`, `_make_mock_store_for_c2`, `_make_pipeline_with_detector`, `_make_stub_store_for_embedding_tests`, `_make_pipeline_for_embedding_tests`, `_make_mock_store_for_b5`, `_make_pipeline_with_store`.
  - Import shared helpers from conftest: `MockEmbedderBackend`, `MockRerankerBackend`, `make_embedder`, `make_reranker`, `make_pipeline` — add explicit imports at the top of the file: `from .conftest import MockEmbedderBackend, MockRerankerBackend, make_embedder, make_reranker, make_pipeline` (relative import is required — the root `tests/conftest.py` inserts `tests/` into sys.path, so bare `from conftest import ...` resolves to the ROOT conftest which does not contain these helpers).
  - **Zero logic changes**: copy functions verbatim. The only permitted edits are removing the shared-helper definitions (now in conftest) and adjusting imports.
  - After writing the file, run collect-only and record the count. This count will be used in the final verification sum.
- **Releasable**: after this task, all ingest tests can be run via `uv run pytest tests/pipeline/test_pipeline_ingest.py -n0 --no-cov`.
- **Tests (TDD)** — `tests/pipeline/test_pipeline_ingest.py`:
  - Collect: count collected tests; record the number.
  - Run: all collected tests pass with no failures in serial mode.
  - No duplicate names: `uv run pytest tests/pipeline/test_pipeline_ingest.py --collect-only -q | awk -F'::' '{print $NF}' | sort | uniq -d` → empty.
  - Checkpoint: `uv run pytest tests/pipeline/test_pipeline_ingest.py -n0 --no-cov -x`

#### Task 2.2 — Create `tests/pipeline/test_pipeline_search.py`
- [x] **File**: `tests/pipeline/test_pipeline_search.py`
- **Depends on**: Task 1.1
- **Description**:
  - Identify all search tests. Search group covers: single-collection `pipeline.search()`, `pipeline.search_with_context()`, `pipeline.explain()`, eval-trace tests, ACL filtering in single-collection search, filter-plus-ACL warning behaviour, result shape verification, namespace-scoped lookups, document list/delete ops, embedder/reranker warmth checks, and embedding-model awareness tests that call search.
  - Key sections to include: `test_search_with_context_records_context_stage`, `test_pipeline_search_with_context_malformed_chunk_id`, `test_pipeline_search_embedder_exception_propagates`, `test_pipeline_search_with_context_fetch_exception_propagates`, all `test_eval_trace_*`, namespace and document tests (`test_get_collection_meta_namespace_param`, `test_search_*`, `test_list_documents_*`, `test_delete_document_*`), filter/ACL warning tests, warmth tests, and embedding-model search tests (`test_search_uses_passed_embedder`, `test_search_does_not_call_global_embedder`, `test_search_with_context_uses_passed_embedder`, `test_search_many_no_embedding_model_attribute_error`, `test_explain_multi_collection_no_embedding_model_attribute_error`, `test_search_many_signature_unchanged`, `test_telemetry_entry_no_query_parameter`, `test_job_status_enum_values_unchanged`).
  - Move section-local helpers: `_make_scored_candidate`, `_make_search_result`.
  - Verify the `test_store_has_vector_index_*` and `test_hybrid_search_with_trace_filters_applied` tests (lines 6490–6508): if their primary assertion is single-collection search behaviour, assign to search; if they are setup tests for multi-collection, assign to multi.
  - Import shared helpers from conftest — add explicit imports at the top of the file: `from .conftest import MockEmbedderBackend, MockRerankerBackend, make_embedder, make_reranker, make_pipeline` (relative import is required — the root `tests/conftest.py` inserts `tests/` into sys.path, so bare `from conftest import ...` resolves to the ROOT conftest which does not contain these helpers).
  - **Zero logic changes**.
- **Releasable**: after this task, all search tests can be run via `uv run pytest tests/pipeline/test_pipeline_search.py -n0 --no-cov`.
- **Tests (TDD)** — `tests/pipeline/test_pipeline_search.py`:
  - Collect: count collected tests; record the number.
  - Run: all collected tests pass with no failures in serial mode.
  - No duplicate names within file.
  - Checkpoint: `uv run pytest tests/pipeline/test_pipeline_search.py -n0 --no-cov -x`

#### Task 2.3 — Create `tests/pipeline/test_pipeline_multi.py`
- [x] **File**: `tests/pipeline/test_pipeline_multi.py`
- **Depends on**: Task 1.1
- **Description**:
  - Identify all multi-collection tests. Multi group covers: `pipeline.search_many()` fanout behaviour, `_fuse_rag_fusion_results` unit tests, RAG fusion integration (search-path), explain multi-collection, and cross-collection assertions.
  - Key sections: all `test_search_many_*`, `test_same_chunk_id_in_two_collections_*`, `test_fuse_rag_fusion_results_*`, `test_search_rag_fusion_*`, `test_search_with_context_rag_fusion_forwarded`, `test_search_many_rag_fusion_*`, `test_explain_rag_fusion_*`, `test_explain_pipeline_result_has_rag_fusion_fields`, `test_search_pipeline_result_has_rag_fusion_fields`, `test_recompute_*` (these call `recompute_collection_meta` — if the ingest-file assignment left none here, check the recompute tests at lines 3981–4175 and confirm they go to ingest per rule 1).
  - Move section-local helpers: `_scored`, `_meta`, `_search_many_pipeline` (lines 3055–3131, unless already in conftest), `_make_candidate`, `_make_rag_fusion_search_many_pipeline`, `_rag_scored`.
  - **Edge case check**: before finalising this file, grep for `connected_store` in its test list. Any test using `connected_store` must be genuine search_many/multi-collection context (e.g. `test_hybrid_search_with_trace_filters_applied` or multi-collection round-trip). If a `connected_store` test is purely single-collection, move it to search or ingest.
  - This file should be predominantly mock-based (no `connected_store` fixture if avoidable), keeping its serial runtime short.
  - Import shared helpers from conftest — add explicit imports at the top of the file: `from .conftest import MockEmbedderBackend, MockRerankerBackend, make_embedder, make_reranker, make_pipeline` (relative import is required — the root `tests/conftest.py` inserts `tests/` into sys.path, so bare `from conftest import ...` resolves to the ROOT conftest which does not contain these helpers).
  - **Zero logic changes**.
- **Releasable**: after this task, all multi-collection tests can be run via `uv run pytest tests/pipeline/test_pipeline_multi.py -n0 --no-cov`.
- **Tests (TDD)** — `tests/pipeline/test_pipeline_multi.py`:
  - Collect: count collected tests; record the number.
  - Sum check: `ingest_count + search_count + multi_count` must equal `BASELINE_COUNT` (recorded in Task 1.1).
  - Run: all collected tests pass with no failures in serial mode.
  - No duplicate names within file.
  - Cross-file duplicate check: `uv run pytest tests/pipeline/ --collect-only -q | awk -F'::' '{print $NF}' | sort | uniq -d` → empty.
  - Checkpoint: `uv run pytest tests/pipeline/test_pipeline_multi.py -n0 --no-cov -x`

---

### Phase 3 — Cleanup
> **Releasable**: after Task 3.1, `tests/test_pipeline.py` is gone and the full suite uses the split files exclusively.

#### Task 3.1 — Delete `tests/test_pipeline.py`
- [ ] **File**: `tests/test_pipeline.py` (deleted)
- **Depends on**: Task 2.1, Task 2.2, Task 2.3 (all three files must be complete and passing before deletion)
- **Description**:
  - Confirm the sum check passed in Task 2.3 (all `BASELINE_COUNT` tests accounted for across the three split files).
  - Run `uv run pytest tests/test_pipeline.py --collect-only -q --no-cov | grep '::' | sed 's/.*:://' | sort > /tmp/pipeline_before.txt && uv run pytest tests/pipeline/ --collect-only -q --no-cov | grep '::' | sed 's/.*:://' | sort > /tmp/pipeline_after.txt && diff /tmp/pipeline_before.txt /tmp/pipeline_after.txt` — diff must be empty (every test name from the original file appears in exactly one split file; no drops, no additions beyond what was in the original).
  - The collected count in `tests/pipeline/` must equal `BASELINE_COUNT` before proceeding to deletion.
  - Move the original file to trash: `trash tests/test_pipeline.py`
  - Run the full suite immediately: `uv run pytest --no-cov` — must pass with 0 failures.
- **Releasable**: after this task, the split is live and `tests/test_pipeline.py` no longer exists.
- **Tests (TDD)**:
  - Full suite passes: `uv run pytest --no-cov`
  - Serial mode passes: `uv run pytest tests/pipeline/ -n0 --no-cov`
  - Collected count matches: `uv run pytest tests/pipeline/ --collect-only -q | tail -1` shows exactly `BASELINE_COUNT` collected tests (the count recorded in Task 1.1 from `uv run pytest tests/test_pipeline.py --collect-only -q | tail -1`).
  - Checkpoint: `uv run pytest --no-cov`

---

### Final Phase — Verification & Documentation

#### Task F.1 — Final verification & documentation update
- [ ] **File**: N/A (agent task) + `Documentation/Architecture/200_testing_strategy.md`
- **Depends on**: Task 3.1
- **Description**:
  - Spawn an agent to discover all documentation in the project that references `tests/test_pipeline.py`, the glob pattern `tests/test_*.py`, or the 'Adding a test' guidance, and update every file whose content is affected. The agent must:
    1. Update `Documentation/Architecture/200_testing_strategy.md`: update the tier-diagram label from `tests/test_*.py` to `tests/**/test_*.py` (documentation label only — pytest already discovers subdirectories via `testpaths = ["tests"]` in `pyproject.toml`; no pytest configuration change is needed) and update the 'Adding a test' guidance to reference `tests/pipeline/` for pipeline tests.
    2. Scan all other Architecture docs, ADRs, and user guides for references to `test_pipeline.py` and update them to name the split files.
    3. Not touch unrelated docs.
  - Verify all acceptance criteria below are met before marking this task complete.
- **Releasable**: after this task, the feature is fully verified and all documentation reflects the delivered implementation.
- **Acceptance criteria** (must all pass):
  - [ ] `uv run pytest tests/pipeline/ --collect-only -q | tail -1` shows exactly `BASELINE_COUNT` collected tests (the count recorded in Task 1.1 from `uv run pytest tests/test_pipeline.py --collect-only -q | tail -1`).
  - [ ] `uv run pytest --no-cov` exits 0 (all tests pass, no failures).
  - [ ] `uv run pytest` exits 0 (coverage gate ≥85% met).
  - [ ] `uv run pytest tests/pipeline/ -n0 --no-cov` exits 0 (CI serial mode passes).
  - [ ] `uv run pytest tests/pipeline/ --collect-only -q | awk -F'::' '{print $NF}' | sort | uniq -d` produces empty output (no cross-file duplicate test function names).
  - [ ] Per-file serial time check: `time uv run pytest tests/pipeline/test_pipeline_ingest.py -n0 --no-cov` should complete in ≤150s on a ≥4-core local machine. (Full-suite parallel wall time is local-dev only; CI uses `-n0` and is unaffected by this split.)
  - [ ] `tests/test_pipeline.py` no longer exists on disk.
  - [ ] `Documentation/Architecture/200_testing_strategy.md` updated: glob pattern and 'Adding a test' section reference `tests/pipeline/`.
- **Tests (TDD)**: N/A — this is a verification and documentation task.
- **Checkpoint**: manually confirm every acceptance criterion above is checked.
